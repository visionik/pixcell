"""Kitty graphics protocol: the escape codes, and the placeholder text that shows an image.

Written from the protocol's documentation
(https://sw.kovidgoyal.net/kitty/graphics-protocol/). Pure functions: nothing here
writes to a terminal.

An image is shown with *Unicode placeholders*. The image is sent once, given a virtual
placement of ``columns x rows`` cells, and then drawn by printing ordinary text: each
cell is U+10EEEE with two combining marks that number its row and column, in a
foreground colour that encodes the image id. To a TUI that's just coloured text, so the
picture scrolls, clips and redraws like the text around it.
"""

from __future__ import annotations

import base64
import random

from pixcell._diacritics import DIACRITICS

PLACEHOLDER = "\U0010eeee"
"""The character each placeholder cell holds."""

MAX_CELLS = len(DIACRITICS)
"""The most rows or columns an image can span (one combining mark per number)."""

CHUNK_BYTES = 4096
"""Payload per escape code: the protocol's limit for chunked transmission."""


def command(keys: dict[str, object], payload: str = "") -> str:
    """One graphics escape code: ``ESC _ G <keys> ; <payload> ESC \\``."""
    control = ",".join(f"{key}={value}" for key, value in keys.items())
    return f"\x1b_G{control};{payload}\x1b\\" if payload else f"\x1b_G{control}\x1b\\"


def transmit(image_id: int, png: bytes, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """Send a PNG for later display (``a=t``), quietly (``q=2``), in chunks.

    A single-chunk transmission carries no ``m`` key at all: some terminals (Rex) have
    shown nothing when a one-shot transmission says ``m=0``.
    """
    if chunk_bytes <= 0 or chunk_bytes % 4:
        raise ValueError("chunk_bytes must be a positive multiple of 4")
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[start : start + chunk_bytes] for start in range(0, len(data), chunk_bytes)] or [""]
    first = {"a": "t", "f": 100, "t": "d", "i": image_id, "q": 2}
    if len(chunks) == 1:
        return command(first, chunks[0])
    codes = [command({**first, "m": 1}, chunks[0])]
    codes += [command({"m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(command({"m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def frame(
    image_id: int, png: bytes, gap: int = 0, *, replace: int | None = None, chunk_bytes: int = CHUNK_BYTES
) -> str:
    """Add an animation frame (``a=f``): a whole PNG shown for ``gap`` milliseconds; or,
    with ``replace=N``, overwrite frame N's pixels in place. Every chunk carries ``a=f``."""
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[start : start + chunk_bytes] for start in range(0, len(data), chunk_bytes)] or [""]
    first: dict[str, object] = {"a": "f", "i": image_id}
    if replace is not None:
        first["r"] = replace
    first.update({"f": 100, "X": 1})
    if replace is None:
        first["z"] = max(1, gap)
    first["q"] = 2
    if len(chunks) == 1:
        return command(first, chunks[0])
    codes = [command({**first, "m": 1}, chunks[0])]
    codes += [command({"a": "f", "m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(command({"a": "f", "m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def pixels(picture, *, compress: bool = True) -> tuple[dict[str, object], bytes]:
    """A Pillow picture as raw pixels for ``f=24``/``f=32``.

    ``compress`` adds zlib (``o=z``). Leave it off unless the terminal's
    ``kitty.fastest_format`` fact is ``zlib``.
    """
    has_alpha = picture.mode in ("RGBA", "LA") or (picture.mode == "P" and "transparency" in picture.info)
    picture = picture.convert("RGBA" if has_alpha else "RGB")
    keys: dict[str, object] = {"f": 32 if has_alpha else 24, "s": picture.width, "v": picture.height}
    return keys, _maybe_compress(picture.tobytes(), keys, compress)


def _chunked(first: dict[str, object], data: bytes, more: dict[str, object], chunk_bytes: int) -> str:
    encoded = base64.standard_b64encode(data).decode("ascii")
    chunks = [encoded[start : start + chunk_bytes] for start in range(0, len(encoded), chunk_bytes)] or [""]
    if len(chunks) == 1:
        return command(first, chunks[0])  # no m key: Rex has shown nothing for a one-shot m=0
    codes = [command({**first, "m": 1}, chunks[0])]
    codes += [command({**more, "m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(command({**more, "m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def transmit_pixels(
    image_id: int,
    picture=None,
    *,
    data: bytes | None = None,
    width: int | None = None,
    height: int | None = None,
    alpha: bool = False,
    compress: bool = True,
    chunk_bytes: int = CHUNK_BYTES,
) -> str:
    """Send a picture as raw pixels for later display (``a=t``).

    ``picture`` is a Pillow image. ``data`` is raw RGB or RGBA (``alpha=True``) of
    ``width`` by ``height``, already decoded, so it is not loaded into Pillow. Pass one
    of the two, not both. ``compress`` adds zlib (``o=z``). The caller turns that on
    only when the terminal's ``kitty.fastest_format`` fact is ``zlib``.
    """
    if data is not None and picture is not None:
        raise ValueError("pass a picture or raw data, not both")
    if data is not None:
        if not width or not height:
            raise ValueError("raw pixels need width and height")
        keys: dict[str, object] = {"f": 32 if alpha else 24, "s": width, "v": height}
        payload = _maybe_compress(data, keys, compress)
        return _chunked({"a": "t", "t": "d", "i": image_id, **keys, "q": 2}, payload, {}, chunk_bytes)
    if picture is None:
        raise ValueError("picture is required")
    keys, payload = pixels(picture, compress=compress)
    return _chunked({"a": "t", "t": "d", "i": image_id, **keys, "q": 2}, payload, {}, chunk_bytes)


def _maybe_compress(data: bytes, keys: dict[str, object], compress: bool) -> bytes:
    if not compress:
        return data
    import zlib

    keys["o"] = "z"
    return zlib.compress(data, 1)


def edit_pixels(
    image_id: int, picture, x: int, y: int, *, frame: int = 1, compress: bool = True, chunk_bytes: int = CHUNK_BYTES
) -> str:
    """Overwrite part of a frame in place with raw pixels. ``compress`` adds zlib."""
    keys, data = pixels(picture, compress=compress)
    first = {"a": "f", "i": image_id, "r": frame, "x": x, "y": y, **keys, "X": 1, "q": 2}
    return _chunked(first, data, {"a": "f"}, chunk_bytes)


def edit(image_id: int, png: bytes, x: int, y: int, *, frame: int = 1, chunk_bytes: int = CHUNK_BYTES) -> str:
    """Overwrite part of a frame in place: the PNG's pixels at (x, y) of frame ``frame``
    (``a=f`` with ``r``; ``X=1`` replaces rather than blends). For live pictures, only
    the part that changed is sent."""
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[start : start + chunk_bytes] for start in range(0, len(data), chunk_bytes)] or [""]
    first = {"a": "f", "i": image_id, "r": frame, "x": x, "y": y, "f": 100, "X": 1, "q": 2}
    if len(chunks) == 1:
        return command(first, chunks[0])
    codes = [command({**first, "m": 1}, chunks[0])]
    codes += [command({"a": "f", "m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(command({"a": "f", "m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def animate(image_id: int, *, run: bool | None = None, frame: int | None = None, gap: int | None = None) -> str:
    """Control an animation (``a=a``): set a frame's gap (``frame`` + ``gap``), and/or
    run it looping forever (``run=True``) or stop it (``run=False``)."""
    keys: dict[str, object] = {"a": "a", "i": image_id}
    if frame is not None:
        keys["r"] = frame
    if gap is not None:
        keys["z"] = max(1, gap)
    if run is not None:
        keys["s"] = 3 if run else 1
        if run:
            keys["v"] = 1  # loop forever
    keys["q"] = 2
    return command(keys)


def place(
    image_id: int,
    columns: int,
    rows: int,
    placement_id: int = 1,
    *,
    source: tuple[int, int, int, int] | None = None,
    offset: tuple[int, int] = (0, 0),
    z: int | None = None,
) -> str:
    """A virtual placement (``U=1``) of ``columns x rows`` cells, for placeholders to show.

    Sending it again with the same ids replaces it (e.g. after a resize). ``source`` is
    the pixel rectangle ``(x, y, width, height)`` of the image to show. ``offset`` is how
    many pixels into the first cell that rectangle starts. ``z`` is the placement's
    z-index. Omitted, the whole image sits at the cell corner.
    """
    _check_size(columns, rows)
    keys: dict[str, object] = {
        "a": "p",
        "U": 1,
        "i": image_id,
        "p": placement_id,
        "c": columns,
        "r": rows,
    }
    if source is not None:
        keys.update(zip("xywh", source))
    if offset[0]:
        keys["X"] = offset[0]
    if offset[1]:
        keys["Y"] = offset[1]
    if z is not None:
        keys["z"] = z
    keys["q"] = 2
    return command(keys)


def place_here(
    image_id: int,
    columns: int,
    rows: int,
    source: tuple[int, int, int, int] | None = None,
    placement_id: int = 1,
) -> str:
    """A placement at the cursor (for terminals without placeholders), over ``columns x
    rows`` cells, showing the ``source`` rectangle (x, y, width, height in pixels) of
    the image. The cursor stays put (``C=1``); the same ids replace an earlier one."""
    _check_size(columns, rows)
    keys: dict[str, object] = {"a": "p", "i": image_id, "p": placement_id, "c": columns, "r": rows}
    if source is not None:
        keys.update(zip("xywh", source))
    keys.update({"C": 1, "q": 2})
    return command(keys)


def delete_placement(image_id: int, placement_id: int = 1) -> str:
    """Remove one placement, keeping the image's data for the next one (``d=i``)."""
    return command({"a": "d", "d": "i", "i": image_id, "p": placement_id, "q": 2})


def delete(image_id: int) -> str:
    """Delete an image's placements and free its data (``d=I``)."""
    return command({"a": "d", "d": "I", "i": image_id, "q": 2})


def query(image_id: int) -> str:
    """Ask whether the terminal supports the protocol: a 1x1 RGB image, not stored.

    A supporting terminal answers ``ESC _ G i=<id> ; OK ESC \\``.
    """
    return command({"i": image_id, "s": 1, "v": 1, "a": "q", "t": "d", "f": 24}, "AAAA")


def id_colour(image_id: int) -> tuple[int, int, int]:
    """The 24-bit foreground colour carrying an image id's low three bytes."""
    return (image_id >> 16) & 0xFF, (image_id >> 8) & 0xFF, image_id & 0xFF


def placeholder_row(image_id: int, row: int, columns: int) -> str:
    """The text for one row of an image: every cell carries its row and column marks
    (and the id's fourth byte, if any), so nothing depends on the inheritance rules."""
    _check_size(columns, row + 1)
    high = image_id >> 24
    high_mark = chr(DIACRITICS[high]) if high else ""
    row_mark = chr(DIACRITICS[row])
    return "".join(PLACEHOLDER + row_mark + chr(DIACRITICS[column]) + high_mark for column in range(columns))


def new_image_id() -> int:
    """A random id that fits in the colour alone (no fourth byte), never zero.

    Random, so two programs sharing a terminal are unlikely to collide.
    """
    return random.randint(0x000100, 0xFFFFFF)


def _check_size(columns: int, rows: int) -> None:
    if not (0 < columns <= MAX_CELLS and 0 < rows <= MAX_CELLS):
        raise ValueError(f"an image spans 1 to {MAX_CELLS} cells each way, not {columns}x{rows}")

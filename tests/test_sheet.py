"""Sheet draws placements with the host's protocol. No terminal and no libghostty."""

from __future__ import annotations

import base64
import zlib

import pytest
from PIL import Image

from pixcell import kitty
from pixcell.probe import Capabilities
from pixcell.sheet import Placement, Sheet

RED = bytes([255, 0, 0])
RGBA = bytes([255, 0, 0, 128])


def placement(**overrides) -> Placement:
    values = dict(
        key="pic",
        pixels=RED,
        pixel_format="rgb",
        pixel_width=1,
        pixel_height=1,
        column=1,
        row=4,
        columns=1,
        rows=1,
    )
    values.update(overrides)
    return Placement(**values)


def commands(codes: str) -> list[dict[str, str]]:
    found = []
    for piece in codes.split("\x1b\\"):
        if "\x1b_G" not in piece:
            continue
        control = piece.split("\x1b_G", 1)[1].split(";", 1)[0]
        found.append(dict(part.split("=", 1) for part in control.split(",") if "=" in part))
    return found


def payload(codes: str) -> bytes:
    body = codes.split("\x1b_G", 1)[1]
    data = body.split(";", 1)[1].split("\x1b\\", 1)[0]
    return zlib.decompress(base64.standard_b64decode(data))


def text_of(drawn_rows) -> dict[int, str]:
    return {row: "".join(segment.text for segment in strip) for row, strip in drawn_rows.items()}


def test_place_defaults_match_the_old_call():
    assert kitty.place(7, 2, 3) == "\x1b_Ga=p,U=1,i=7,p=1,c=2,r=3,q=2\x1b\\"


def test_place_source_offset_and_z():
    code = kitty.place(7, 2, 3, source=(1, 2, 3, 4), offset=(5, 6), z=9)
    keys = commands(code)[0]
    assert keys["x"] == "1" and keys["y"] == "2" and keys["w"] == "3" and keys["h"] == "4"
    assert keys["X"] == "5" and keys["Y"] == "6" and keys["z"] == "9"
    assert keys["U"] == "1"


def test_transmit_pixels_raw_rgb_and_rgba():
    rgb = kitty.transmit_pixels(4, data=RED, width=1, height=1)
    keys = commands(rgb)[0]
    assert keys["f"] == "24" and keys["o"] == "z" and keys["s"] == "1"
    assert payload(rgb) == RED

    rgba = kitty.transmit_pixels(4, data=RGBA, width=1, height=1, alpha=True)
    assert commands(rgba)[0]["f"] == "32"
    assert payload(rgba) == RGBA


def test_transmit_pixels_pillow_still_works():
    code = kitty.transmit_pixels(4, Image.new("RGB", (1, 1), (1, 2, 3)))
    assert commands(code)[0]["f"] == "24"
    assert payload(code) == bytes([1, 2, 3])


def test_transmit_pixels_rejects_both_inputs():
    with pytest.raises(ValueError):
        kitty.transmit_pixels(1, Image.new("RGB", (1, 1)), data=RED, width=1, height=1)


def test_kitty_sheet_uploads_once_and_paints_placeholders():
    sheet = Sheet(Capabilities("kitty", (8, 16), terminal="rex"))
    first = sheet.update([placement(source=(0, 0, 1, 1), offset=(2, 3), z=1)])
    assert first.overlay == ""
    uploaded = commands(first.setup)
    assert uploaded[0]["a"] == "t" and uploaded[0]["o"] == "z"
    assert uploaded[1]["a"] == "p" and uploaded[1]["x"] == "0" and uploaded[1]["X"] == "2"
    assert uploaded[1]["z"] == "1"
    assert "\U0010eeee" in text_of(first.rows)[4]
    assert text_of(first.rows)[4].startswith(" ")

    again = sheet.update([placement(source=(0, 0, 1, 1), offset=(2, 3), z=1)])
    assert again.setup == ""
    assert "\U0010eeee" in text_of(again.rows)[4]

    changed = sheet.update([placement(generation=1)])
    assert any(item.get("a") == "t" for item in commands(changed.setup))

    gone = sheet.update([])
    assert commands(gone.setup)[0]["a"] == "d"
    assert gone.rows == {}


def test_live_frames_follow_the_fact():
    from pixcell import textual as pixcell_textual
    from pixcell.live import LiveImage

    previous = pixcell_textual.capabilities()
    picture = Image.new("RGB", (1, 1), (4, 5, 6))
    try:
        pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="iterm2"))
        png = LiveImage._whole(None, 3, picture)
        assert commands(png)[0]["f"] == "100"
        pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))
        compressed = LiveImage._whole(None, 3, picture)
        assert commands(compressed)[0]["o"] == "z"
        patched = kitty.edit_pixels(3, picture, 0, 0, compress=False)
        assert "o" not in commands(patched)[0]
    finally:
        pixcell_textual.configure(previous)


def test_fastest_format_comes_from_the_fact():
    assert Capabilities("kitty", terminal="rex").fastest_format == "zlib"
    assert Capabilities("kitty", terminal="iterm2").fastest_format == "png"
    assert Capabilities("kitty", terminal="no-such-terminal").fastest_format == "png"


def test_zlib_follows_the_terminal_fact():
    unknown = Sheet(Capabilities("kitty", (8, 16), terminal="no-such-terminal"))
    drawn = unknown.update([placement()])
    assert commands(drawn.setup)[0]["f"] == "100"
    assert "o" not in commands(drawn.setup)[0]

    class Raw(Capabilities):
        def fact(self, key, default=None):
            if key == "kitty.fastest_format":
                return "raw"
            return super().fact(key, default)

    drawn = Sheet(Raw("kitty", (8, 16))).update([placement()])
    keys = commands(drawn.setup)[0]
    assert keys["f"] == "24" and "o" not in keys
    body = drawn.setup.split("\x1b_G", 1)[1]
    data = body.split(";", 1)[1].split("\x1b\\", 1)[0]
    assert base64.standard_b64decode(data) == RED


def test_kitty_png_format_uses_transmit():
    image = Image.new("RGB", (1, 1), (9, 9, 9))
    buffer = __import__("io").BytesIO()
    image.save(buffer, format="PNG")
    sheet = Sheet(Capabilities("kitty", (8, 16), terminal="iterm2"))
    drawn = sheet.update([placement(pixels=buffer.getvalue(), pixel_format="png")])
    assert commands(drawn.setup)[0]["f"] == "100"


def test_half_blocks_are_cell_text():
    sheet = Sheet(Capabilities("halfblock", (8, 16)))
    drawn = sheet.update([placement()])
    assert drawn.setup == "" and drawn.overlay == ""
    assert "▀" in text_of(drawn.rows)[4]


def test_iterm2_overlay_follows_the_origin():
    sheet = Sheet(Capabilities("iterm2", (8, 16), terminal="iterm2"))
    drawn = sheet.update([placement()], origin=(2, 3))
    assert drawn.rows == {} and drawn.setup == ""
    assert drawn.overlay.startswith("\x1b[8;4H")
    assert "\x1b]1337;File=" in drawn.overlay

    moved = sheet.update([placement()], origin=(0, 0))
    assert moved.overlay.startswith("\x1b[5;2H")


def test_sixel_overlay_moves_the_cursor():
    sheet = Sheet(Capabilities("sixel", (8, 16), terminal="foot"))
    drawn = sheet.update([placement(column=0, row=0, columns=2, rows=2)], origin=(1, 1))
    assert drawn.overlay.startswith("\x1b[2;2H")
    assert "\x1bP" in drawn.overlay


def test_host_returns_placements_and_writes_nothing():
    sheet = Sheet(Capabilities("host"))
    item = placement()
    drawn = sheet.update([item])
    assert drawn.setup == "" and drawn.overlay == "" and drawn.rows == {}
    assert drawn.placements == [item]


def test_paint_false_keeps_the_upload_without_a_row():
    sheet = Sheet(Capabilities("kitty", (8, 16), terminal="rex"))
    first = sheet.update([placement()])
    assert first.rows and "a=t" in first.setup
    hidden = sheet.update([placement(paint=False)])
    assert hidden.rows == {} and hidden.setup == ""
    gone = sheet.update([])
    assert "a=d" in gone.setup


def test_off_writes_nothing():
    sheet = Sheet(Capabilities("off"))
    drawn = sheet.update([placement()])
    assert drawn.setup == "" and drawn.overlay == "" and drawn.rows == {}
    assert drawn.placements == []


def _image_pixels(width: int, height: int) -> bytes:
    return bytes(RED * (width * height))


def test_a_cropped_picture_is_uploaded_whole_and_cropped_once():
    """The placement applies ``source``; the upload must not be cropped too."""
    sheet = Sheet(Capabilities("kitty", (8, 16), terminal="rex"))
    pic = placement(pixels=_image_pixels(20, 10), pixel_width=20, pixel_height=10, source=(10, 0, 10, 10))
    upload, place = commands(sheet.update([pic]).setup)
    assert (upload["s"], upload["v"]) == ("20", "10")  # the whole image
    assert (place["x"], place["y"], place["w"], place["h"]) == ("10", "0", "10", "10")


def test_a_new_crop_offset_or_layer_places_the_upload_again():
    sheet = Sheet(Capabilities("kitty", (8, 16), terminal="rex"))
    pic = placement(pixels=_image_pixels(20, 10), pixel_width=20, pixel_height=10)
    sheet.update([pic])
    assert sheet.update([pic]).setup == ""  # nothing changed
    for changed in (
        Placement(**{**pic.__dict__, "source": (0, 0, 5, 5)}),
        Placement(**{**pic.__dict__, "source": (0, 0, 5, 5), "offset": (2, 3)}),
        Placement(**{**pic.__dict__, "source": (0, 0, 5, 5), "offset": (2, 3), "z": 4}),
    ):
        [place] = commands(sheet.update([changed]).setup)
        assert place["a"] == "p"  # placed again, not uploaded again


def test_kitty_direct_crops_in_the_image_s_pixels_not_the_host_s_cells():
    """A 100-pixel-wide picture over ten 8-pixel cells, fully visible, shows all 100."""
    sheet = Sheet(Capabilities("kitty-direct", (8, 16), terminal="kitty"))
    pic = placement(pixels=_image_pixels(100, 40), pixel_width=100, pixel_height=40, column=0, row=0, columns=10, rows=2)
    sheet.update([pic])
    [shown] = commands(sheet.update([pic], origin=(0, 0)).overlay)
    assert (shown["x"], shown["y"], shown["w"], shown["h"]) == ("0", "0", "100", "40")
    [half] = commands(sheet.update([pic], origin=(0, 0), clip=(5, 0, 5, 2)).overlay)
    assert (half["x"], half["w"]) == ("50", "50")  # the right half of the image

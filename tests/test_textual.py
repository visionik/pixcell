from PIL import Image as PILImage
from textual.app import App, ComposeResult

import pixcell.textual as ct
from pixcell import kitty
from pixcell.probe import Capabilities


class Recorder:
    def __init__(self):
        self.codes = []

    def write(self, data):
        self.codes.append(data)

    def flush(self):
        pass


def _app(widget, recorder):
    class ImageApp(App):
        CSS = "Image { width: 10; height: 4; }"

        def compose(self) -> ComposeResult:
            yield widget

        def on_mount(self):
            original = self._driver.write
            self._driver.write = lambda data: (recorder.write(data) if data.startswith("\x1b_G") else original(data))

    return ImageApp()


async def test_kitty_image_sends_once_draws_placeholders_and_deletes():
    ct.configure(Capabilities("kitty", (8, 16)))
    picture = PILImage.new("RGB", (400, 300), (200, 30, 30))
    widget = ct.Image(picture)
    recorder = Recorder()
    app = _app(widget, recorder)
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.pause(0.05)
            if widget._sent_box:
                break
        sent = "".join(recorder.codes)
        image_id = widget._image_id
        assert f"a=t,f=100,t=d,i={image_id},q=2" in sent
        assert f"a=p,U=1,i={image_id},p=1,c=10,r=4,q=2" in sent
        assert widget._sent_box == (80, 64)  # 10x4 cells of 8x16: the copy sent fits that
        line = widget.render_line(2)
        assert line.text == kitty.placeholder_row(image_id, 2, 10)
        recorder.codes.clear()
        await widget.remove()
        assert kitty.delete(image_id) in "".join(recorder.codes)
    ct.configure(Capabilities("halfblock"))


async def test_registry_evicts_pictures_not_drawn_lately():
    registry = ct._Registry()
    registry.budget = 100

    class Fake:
        evicted = False

        def _evicted(self):
            self.evicted = True

    old, recent = Fake(), Fake()
    registry.add(1, 60, old)
    registry.add(2, 30, recent)
    registry._sent[1] = (60, 0.0, old)  # drawn long ago
    assert registry.make_room(40, keep=3) == [1]
    assert old.evicted and not recent.evicted and registry.used == 30


async def test_halfblock_widget_draws_colours():
    ct.configure(Capabilities("halfblock"))
    widget = ct.Image(PILImage.new("RGB", (40, 40), (0, 128, 0)))
    app = _app(widget, Recorder())
    async with app.run_test() as pilot:
        await pilot.pause()
        line = widget.render_line(0)
        assert line.text == "▀" * 10
        assert {segment.style.color.triplet for segment in line} == {(0, 128, 0)}


async def test_kitty_static_image_uses_zlib_and_large_chunks_on_fast_terminals(monkeypatch):
    import base64
    import random
    import re
    import zlib

    # Noisy image so compressed payload exceeds 4096 B base64, proving chunk_bytes > 4096 is used.
    rng = random.Random(42)
    raw = bytes(rng.getrandbits(8) for _ in range(160 * 128 * 3))
    picture = PILImage.frombytes("RGB", (160, 128), raw)

    ct.configure(Capabilities("kitty", (16, 32), terminal="ghostty", version="1.4.0"))
    try:
        widget = ct.Image(picture)
        recorder = Recorder()
        app = _app(widget, recorder)
        async with app.run_test() as pilot:
            for _ in range(40):
                await pilot.pause(0.05)
                if widget._sent_box:
                    break
            sent = "".join(recorder.codes)
            image_id = widget._image_id
            # Ghostty has max_escape_bytes = 65 MiB -> single a=t APC (no m=1 chunking) with o=z
            apcs = re.findall(r"\x1b_G([^;]*)(?:;([^\x1b]*))?\x1b\\", sent)
            tx = [(k, p) for k, p in apcs if "a=t" in k]
            assert len(tx) == 1
            keys, payload = tx[0]
            assert f"i={image_id}" in keys
            assert "o=z" in keys
            assert "f=24" in keys
            assert "m=" not in keys
            # Compressed payload must decompress to exact raw RGB bytes
            decoded = zlib.decompress(base64.standard_b64decode(payload))
            assert decoded == raw
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_kitty_static_image_keeps_png_on_iterm2():
    ct.configure(Capabilities("kitty", (8, 16), terminal="iterm2", version="3.7.7"))
    try:
        widget = ct.Image(PILImage.new("RGB", (80, 64), (30, 120, 200)))
        recorder = Recorder()
        app = _app(widget, recorder)
        async with app.run_test() as pilot:
            for _ in range(40):
                await pilot.pause(0.05)
                if widget._sent_box:
                    break
            sent = "".join(recorder.codes)
            assert f"a=t,f=100,t=d,i={widget._image_id},q=2" in sent
    finally:
        ct.configure(Capabilities("halfblock"))
